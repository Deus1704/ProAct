import tempfile
import unittest
from pathlib import Path

from proactive_assistant_app import database as db
from proactive_assistant_app import uber_client


class UberHistoryQualityTests(unittest.TestCase):
    def test_parse_activity_trip_does_not_invent_pickup_or_ride_type(self) -> None:
        ride = uber_client._parse_activity_trip(
            {
                "uuid": "trip-123",
                "title": "IIT Gandhinagar",
                "subtitle": "Mar 28 • 3:53 AM",
                "description": "₹145.87",
                "cardURL": "https://riders.uber.com/trips/trip-123",
            }
        )

        self.assertIsNotNone(ride)
        assert ride is not None
        self.assertIsNone(ride["pickup_address"])
        self.assertEqual(ride["dropoff_address"], "IIT Gandhinagar")
        self.assertIsNone(ride["ride_type"])

    def test_merge_accepts_completed_trip_without_invented_ride_type(self) -> None:
        summary = {
            "external_ride_id": "trip-123",
            "source_platform": "uber",
            "pickup_address": None,
            "dropoff_address": "IIT Gandhinagar",
            "ride_type": None,
            "is_canceled": False,
            "trip_status": "completed",
        }
        detail = {
            "external_ride_id": "trip-123",
            "source_platform": "uber",
            "pickup_address": "Hostel block",
            "dropoff_address": "IIT Gandhinagar",
            "ride_type": None,
            "is_canceled": False,
            "trip_status": "completed",
            "price": 145.87,
        }

        merged = uber_client._build_verified_trip_record(summary, detail)

        self.assertTrue(uber_client._ride_has_actual_history_fields(merged))
        self.assertEqual(merged["pickup_address"], "Hostel block")
        self.assertIsNone(merged["ride_type"])
        self.assertEqual(merged["price"], 145.87)

    def test_extract_route_block_addresses_from_detail_text(self) -> None:
        pickup, dropoff = uber_client._extract_route_stop_addresses(
            """Uber
Your Trip
6:17 AM, Monday March 23 2026 with ARJUNJI
Trip rating
Trip details

11.79 kilometres

20 minutes

₹145.87

Cash

View Receipt
Resend Receipt
Request Invoice
Route
Kudasan-Por Rd, Kudasan, Gujarat 382419, India

6:17 AM

Indian Institute of Technology, Palaj, Gujarat 382055, India

6:38 AM

Get Help"""
        )

        self.assertEqual(pickup, "Kudasan-Por Rd, Kudasan, Gujarat 382419, India")
        self.assertEqual(dropoff, "Indian Institute of Technology, Palaj, Gujarat 382055, India")

    def test_string_external_ids_are_deduped_deterministically(self) -> None:
        original_db_path = db.DB_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            db.DB_PATH = str(Path(tmpdir) / "assistant.db")
            db.init_db()

            ride = {
                "external_ride_id": "trip-uuid-123",
                "source_platform": "uber",
                "pickup_address": "Hostel block",
                "dropoff_address": "IIT Gandhinagar",
                "ride_type": "Uber Auto",
                "request_timestamp": "2026-03-28T03:53:00+00:00",
                "price": 145.87,
            }

            db.insert_ride(ride)
            db.insert_ride(ride)

            rows = db.get_ride_history(limit=10, source_platform="uber")
            self.assertEqual(len(rows), 1)
        db.DB_PATH = original_db_path

    def test_canceled_and_unfulfilled_summary_rides_are_skipped_before_detail(self) -> None:
        canceled = uber_client._parse_activity_trip(
            {
                "uuid": "trip-cancel",
                "title": "Stop A",
                "subtitle": "Mar 28 • 3:53 AM",
                "description": "₹0.00 • Canceled",
                "cardURL": "https://riders.uber.com/trips/trip-cancel",
            }
        )
        unfulfilled = uber_client._parse_activity_trip(
            {
                "uuid": "trip-unfulfilled",
                "title": "Stop B",
                "subtitle": "May 1 • 9:33 PM",
                "description": "₹0.00 • Unfulfilled",
                "cardURL": "https://riders.uber.com/trips/trip-unfulfilled",
            }
        )

        self.assertEqual(canceled["trip_status"], "canceled")
        self.assertTrue(uber_client._should_skip_summary_trip(canceled))
        self.assertEqual(unfulfilled["trip_status"], "unfulfilled")
        self.assertTrue(uber_client._should_skip_summary_trip(unfulfilled))

    def test_delete_low_fidelity_keeps_valid_address_fare_rides(self) -> None:
        original_db_path = db.DB_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            db.DB_PATH = str(Path(tmpdir) / "assistant.db")
            db.init_db()

            # Should be kept: meaningful destination and fare even without coordinates/ride_type.
            db.insert_ride(
                {
                    "external_ride_id": "keep-1",
                    "source_platform": "uber",
                    "dropoff_address": "Indian Institute of Technology, Palaj, Gujarat 382055, India",
                    "request_timestamp": "2026-03-23T06:17:00+00:00",
                    "price": 145.87,
                    "ride_type": None,
                }
            )

            # Should be deleted: placeholder row with no useful fields.
            db.insert_ride(
                {
                    "external_ride_id": "drop-1",
                    "source_platform": "uber",
                    "dropoff_address": "",
                    "request_timestamp": "2026-03-23T06:18:00+00:00",
                    "price": None,
                    "ride_type": None,
                }
            )

            deleted = db.delete_low_fidelity_uber_rides()
            self.assertEqual(deleted, 1)

            rows = db.get_ride_history(limit=10, source_platform="uber")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["dest_label"], "Indian Institute of Technology, Palaj, Gujarat 382055, India")
        db.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
