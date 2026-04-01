"""SQLite persistence layer for the Proactive Ride Assistant."""

import json
import sqlite3
import os
import time
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "assistant.db")


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


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS ride_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_ride_id TEXT,
                source_platform TEXT DEFAULT 'uber',
                pickup_address TEXT,
                dropoff_address TEXT,
                pickup_lat REAL,
                pickup_lng REAL,
                dropoff_lat REAL,
                dropoff_lng REAL,
                request_timestamp TEXT,
                weekday INTEGER,
                hour_of_day INTEGER,
                ride_type TEXT DEFAULT 'UberX',
                price REAL,
                duration_minutes REAL,
                distance_miles REAL,
                pickup_eta_minutes REAL,
                surge_multiplier REAL DEFAULT 1.0,
                raw_payload TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS ride_interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                timestamp TEXT DEFAULT (datetime('now')),
                suggestion_payload TEXT,
                edited_fields TEXT,
                route_key TEXT
            );

            CREATE TABLE IF NOT EXISTS ride_feedback_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_identifier TEXT NOT NULL,
                route_key TEXT NOT NULL,
                pickup_address TEXT,
                dropoff_address TEXT,
                reason_code TEXT,
                feedback_text TEXT,
                dismiss_count INTEGER NOT NULL DEFAULT 1,
                suggestion_payload TEXT,
                last_seen_at TEXT DEFAULT (datetime('now')),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_identifier, route_key)
            );

            CREATE TABLE IF NOT EXISTS food_order_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_order_id TEXT,
                source_platform TEXT DEFAULT 'swiggy',
                restaurant_name TEXT,
                item_name TEXT,
                cuisine TEXT,
                order_timestamp TEXT,
                weekday INTEGER,
                hour_of_day INTEGER,
                status TEXT,
                price REAL,
                eta_minutes REAL,
                delivery_address TEXT,
                raw_payload TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS food_interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                timestamp TEXT DEFAULT (datetime('now')),
                suggestion_payload TEXT,
                edited_fields TEXT,
                route_key TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_ride_history_weekday_hour
                ON ride_history(weekday, hour_of_day);
            CREATE INDEX IF NOT EXISTS idx_ride_history_timestamp
                ON ride_history(request_timestamp);
            CREATE INDEX IF NOT EXISTS idx_interactions_action
                ON ride_interactions(action_type);
            CREATE INDEX IF NOT EXISTS idx_interactions_route
                ON ride_interactions(route_key);
            CREATE INDEX IF NOT EXISTS idx_ride_feedback_user_updated
                ON ride_feedback_memory(user_identifier, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ride_feedback_route_user
                ON ride_feedback_memory(route_key, user_identifier);
            CREATE INDEX IF NOT EXISTS idx_food_history_weekday_hour
                ON food_order_history(weekday, hour_of_day);
            CREATE INDEX IF NOT EXISTS idx_food_history_timestamp
                ON food_order_history(order_timestamp);
            CREATE INDEX IF NOT EXISTS idx_food_history_source
                ON food_order_history(source_platform);
            CREATE INDEX IF NOT EXISTS idx_food_interactions_action
                ON food_interactions(action_type);
            CREATE INDEX IF NOT EXISTS idx_food_interactions_route
                ON food_interactions(route_key);
        """)


# --- Settings helpers ---

def get_setting(key: str, default=None) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value),
        )


def get_all_settings() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# --- Ride history helpers ---

def insert_ride(ride: dict):
    ts = ride.get("request_timestamp", datetime.utcnow().isoformat())
    if isinstance(ts, (int, float)):
        dt = datetime.utcfromtimestamp(ts)
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace("+00:00", ""))
    weekday = dt.weekday()
    hour = dt.hour
    external_ride_id = ride.get("external_ride_id")
    source_platform = ride.get("source_platform", "uber")

    with get_db() as conn:
        if external_ride_id:
            existing = conn.execute(
                """SELECT 1 FROM ride_history
                   WHERE external_ride_id = ? AND source_platform = ?
                   LIMIT 1""",
                (external_ride_id, source_platform),
            ).fetchone()
            if existing:
                return

        conn.execute(
            """INSERT INTO ride_history
               (external_ride_id, source_platform, pickup_address, dropoff_address,
                pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                request_timestamp, weekday, hour_of_day, ride_type,
                price, duration_minutes, distance_miles, pickup_eta_minutes,
                surge_multiplier, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                external_ride_id,
                source_platform,
                ride.get("pickup_address"),
                ride.get("dropoff_address"),
                ride.get("pickup_lat"),
                ride.get("pickup_lng"),
                ride.get("dropoff_lat"),
                ride.get("dropoff_lng"),
                dt.isoformat(),
                weekday,
                hour,
                ride.get("ride_type", "UberX"),
                ride.get("price"),
                ride.get("duration_minutes"),
                ride.get("distance_miles"),
                ride.get("pickup_eta_minutes"),
                ride.get("surge_multiplier", 1.0),
                json.dumps(ride.get("raw_payload")) if ride.get("raw_payload") else None,
            ),
        )


def get_ride_history(
    limit: int = 100,
    offset: int = 0,
    source_platform: str | None = None,
) -> list[dict]:
    with get_db() as conn:
        if source_platform:
            rows = conn.execute(
                """SELECT * FROM ride_history
                   WHERE source_platform = ?
                   ORDER BY request_timestamp DESC
                   LIMIT ? OFFSET ?""",
                (source_platform, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ride_history ORDER BY request_timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def get_rides_by_weekday_hour(weekday: int, hour_start: int, hour_end: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM ride_history
               WHERE weekday = ? AND hour_of_day BETWEEN ? AND ?
               ORDER BY request_timestamp DESC""",
            (weekday, hour_start, hour_end),
        ).fetchall()
        return [dict(r) for r in rows]


def get_route_frequencies() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT pickup_address, dropoff_address, weekday,
                      AVG(hour_of_day) as avg_hour,
                      COUNT(*) as frequency,
                      AVG(price) as avg_price,
                      AVG(duration_minutes) as avg_duration,
                      MAX(request_timestamp) as last_used,
                      ride_type
               FROM ride_history
               GROUP BY pickup_address, dropoff_address, weekday
               ORDER BY frequency DESC""",
        ).fetchall()
        return [dict(r) for r in rows]


def get_ride_count(source_platform: str | None = None) -> int:
    with get_db() as conn:
        if source_platform:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM ride_history WHERE source_platform = ?",
                (source_platform,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM ride_history").fetchone()
        return row["cnt"]


# --- Food history helpers ---

def _normalize_timestamp(ts) -> datetime:
    if isinstance(ts, (int, float)):
        return datetime.utcfromtimestamp(ts)
    if not ts:
        return datetime.utcnow()
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace("+00:00", ""))


def insert_food_order(order: dict):
    dt = _normalize_timestamp(order.get("order_timestamp"))
    weekday = dt.weekday()
    hour = dt.hour
    external_order_id = order.get("external_order_id")
    source_platform = order.get("source_platform", "swiggy")

    with get_db() as conn:
        if external_order_id:
            existing = conn.execute(
                """SELECT 1 FROM food_order_history
                   WHERE external_order_id = ? AND source_platform = ?
                   LIMIT 1""",
                (external_order_id, source_platform),
            ).fetchone()
            if existing:
                return

        conn.execute(
            """INSERT INTO food_order_history
               (external_order_id, source_platform, restaurant_name, item_name,
                cuisine, order_timestamp, weekday, hour_of_day, status,
                price, eta_minutes, delivery_address, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                external_order_id,
                source_platform,
                order.get("restaurant_name"),
                order.get("item_name"),
                order.get("cuisine"),
                dt.isoformat(),
                weekday,
                hour,
                order.get("status", "delivered"),
                order.get("price"),
                order.get("eta_minutes"),
                order.get("delivery_address"),
                json.dumps(order.get("raw_payload")) if order.get("raw_payload") else None,
            ),
        )


def get_food_order_history(
    limit: int = 100,
    offset: int = 0,
    source_platform: str | None = None,
) -> list[dict]:
    with get_db() as conn:
        if source_platform:
            rows = conn.execute(
                """SELECT * FROM food_order_history
                   WHERE source_platform = ?
                   ORDER BY order_timestamp DESC
                   LIMIT ? OFFSET ?""",
                (source_platform, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM food_order_history ORDER BY order_timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def get_food_order_count(source_platform: str | None = None) -> int:
    with get_db() as conn:
        if source_platform:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM food_order_history WHERE source_platform = ?",
                (source_platform,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM food_order_history").fetchone()
        return row["cnt"]


def get_food_patterns() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT restaurant_name, item_name, cuisine, weekday,
                      AVG(hour_of_day) as avg_hour,
                      COUNT(*) as frequency,
                      AVG(price) as avg_price,
                      AVG(eta_minutes) as avg_eta,
                      MAX(order_timestamp) as last_used,
                      source_platform
               FROM food_order_history
               GROUP BY restaurant_name, item_name, cuisine, weekday, source_platform
               ORDER BY frequency DESC, last_used DESC""",
        ).fetchall()
        return [dict(r) for r in rows]


# --- Food interaction helpers ---

def log_food_interaction(action_type: str, suggestion_payload: dict = None,
                         edited_fields: dict = None, route_key: str = None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO food_interactions (action_type, suggestion_payload, edited_fields, route_key)
               VALUES (?, ?, ?, ?)""",
            (
                action_type,
                json.dumps(suggestion_payload) if suggestion_payload else None,
                json.dumps(edited_fields) if edited_fields else None,
                route_key,
            ),
        )


def get_recent_food_interactions(action_type: str = None, hours: int = 24) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        if action_type:
            rows = conn.execute(
                """SELECT * FROM food_interactions
                   WHERE action_type = ? AND timestamp >= ?
                   ORDER BY timestamp DESC""",
                (action_type, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM food_interactions
                   WHERE timestamp >= ?
                   ORDER BY timestamp DESC""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_food_dismissal_count_for_route(route_key: str, hours: int = 72) -> int:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM food_interactions
               WHERE action_type = 'dismiss' AND route_key = ? AND timestamp >= ?""",
            (route_key, cutoff),
        ).fetchone()
        return row["cnt"]


# --- Interaction helpers ---

def log_interaction(action_type: str, suggestion_payload: dict = None,
                    edited_fields: dict = None, route_key: str = None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO ride_interactions (action_type, suggestion_payload, edited_fields, route_key)
               VALUES (?, ?, ?, ?)""",
            (
                action_type,
                json.dumps(suggestion_payload) if suggestion_payload else None,
                json.dumps(edited_fields) if edited_fields else None,
                route_key,
            ),
        )


def get_recent_interactions(action_type: str = None, hours: int = 24) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        if action_type:
            rows = conn.execute(
                """SELECT * FROM ride_interactions
                   WHERE action_type = ? AND timestamp >= ?
                   ORDER BY timestamp DESC""",
                (action_type, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM ride_interactions
                   WHERE timestamp >= ?
                   ORDER BY timestamp DESC""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_dismissal_count_for_route(route_key: str, hours: int = 72) -> int:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM ride_interactions
               WHERE action_type = 'dismiss' AND route_key = ? AND timestamp >= ?""",
            (route_key, cutoff),
        ).fetchone()
        return row["cnt"]


def get_confirmation_count_for_route(route_key: str) -> int:
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM ride_interactions
               WHERE action_type = 'confirm' AND route_key = ?""",
            (route_key,),
        ).fetchone()
        return row["cnt"]


def upsert_ride_feedback_memory(
    user_identifier: str,
    route_key: str,
    pickup_address: str | None = None,
    dropoff_address: str | None = None,
    reason_code: str | None = None,
    feedback_text: str | None = None,
    suggestion_payload: dict | None = None,
) -> dict:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO ride_feedback_memory (
                user_identifier,
                route_key,
                pickup_address,
                dropoff_address,
                reason_code,
                feedback_text,
                dismiss_count,
                suggestion_payload,
                last_seen_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, datetime('now'), datetime('now'))
            ON CONFLICT(user_identifier, route_key) DO UPDATE SET
                pickup_address = COALESCE(excluded.pickup_address, ride_feedback_memory.pickup_address),
                dropoff_address = COALESCE(excluded.dropoff_address, ride_feedback_memory.dropoff_address),
                reason_code = COALESCE(excluded.reason_code, ride_feedback_memory.reason_code),
                feedback_text = CASE
                    WHEN excluded.feedback_text IS NOT NULL AND excluded.feedback_text != ''
                        THEN excluded.feedback_text
                    ELSE ride_feedback_memory.feedback_text
                END,
                suggestion_payload = COALESCE(excluded.suggestion_payload, ride_feedback_memory.suggestion_payload),
                dismiss_count = ride_feedback_memory.dismiss_count + 1,
                last_seen_at = datetime('now'),
                updated_at = datetime('now')
            """,
            (
                user_identifier,
                route_key,
                pickup_address,
                dropoff_address,
                reason_code,
                feedback_text,
                json.dumps(suggestion_payload) if suggestion_payload else None,
            ),
        )
        row = conn.execute(
            """SELECT * FROM ride_feedback_memory
               WHERE user_identifier = ? AND route_key = ?""",
            (user_identifier, route_key),
        ).fetchone()
        return dict(row) if row else {}


def get_ride_feedback_memory(user_identifier: str, route_key: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM ride_feedback_memory
               WHERE user_identifier = ? AND route_key = ?""",
            (user_identifier, route_key),
        ).fetchone()
        return dict(row) if row else None


def get_recent_ride_feedback_memories(user_identifier: str, limit: int = 12) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM ride_feedback_memory
               WHERE user_identifier = ?
               ORDER BY updated_at DESC
               LIMIT ?""",
            (user_identifier, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_dismissed_route_keys_for_user(user_identifier: str, hours: int = 168) -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT route_key FROM ride_feedback_memory
               WHERE user_identifier = ? AND updated_at >= datetime('now', ?)
               ORDER BY updated_at DESC""",
            (user_identifier, f"-{int(hours)} hours"),
        ).fetchall()
        return [str(r["route_key"]) for r in rows if r["route_key"]]
