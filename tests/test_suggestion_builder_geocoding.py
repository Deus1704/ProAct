import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from proactive_assistant_app.suggestion_builder import SuggestionBuilder


class SuggestionBuilderGeocodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_ride_suggestion_geocodes_origin_and_destination_before_live_quote(self) -> None:
        builder = SuggestionBuilder()

        pattern = {
            "id": 12,
            "destination_id": 44,
            "hour_bin": 24,
            "confidence": 0.9,
            "ride_type": "Uber Go",
        }
        last_ride = {
            "origin_label": "Hostel block",
            "dest_label": "Indian Institute of Technology, Palaj, Gujarat 382055, India",
            "origin_lat": None,
            "origin_lng": None,
            "dest_lat": None,
            "dest_lng": None,
            "departure_time": "2026-03-23T06:17:00+00:00",
        }

        geocode_map = {
            "Hostel block": (23.0, 72.0),
            "Indian Institute of Technology, Palaj, Gujarat 382055, India": (23.1, 72.1),
        }

        with (
            patch("proactive_assistant_app.suggestion_builder.db.get_pattern_row", return_value=("departure_patterns:12", pattern)),
            patch("proactive_assistant_app.suggestion_builder.db.get_destination_cluster", return_value=None),
            patch("proactive_assistant_app.suggestion_builder.db.get_rides_for_destination", return_value=[last_ride]),
            patch.object(builder, "_forward_geocode", side_effect=lambda label: geocode_map[label]),
            patch.object(
                builder,
                "fetch_ride_options",
                new=AsyncMock(
                    return_value=[
                        {
                            "platform": "uber",
                            "ride_type": "Uber Go",
                            "price": 120.0,
                            "eta": 8,
                            "surge_multiplier": 1.0,
                            "raw_price_text": "₹120",
                            "live_data": True,
                        }
                    ]
                ),
            ) as fetch_mock,
        ):
            suggestion = await builder.build_ride_suggestion(
                {
                    "type": "ride",
                    "pattern_ref": "departure_patterns:12",
                    "trigger_reason": ["forecast_pattern"],
                    "confidence": 0.9,
                    "fired_at": datetime(2026, 4, 1, 6, 0, tzinfo=timezone.utc),
                    "suppressed": False,
                    "suppression_reason": None,
                    "early_departure_delta": 0,
                }
            )

        origin_arg, destination_arg = fetch_mock.await_args.args
        self.assertEqual(origin_arg["lat"], 23.0)
        self.assertEqual(origin_arg["lng"], 72.0)
        self.assertEqual(destination_arg["lat"], 23.1)
        self.assertEqual(destination_arg["lng"], 72.1)
        self.assertEqual(suggestion["origin"]["lat"], 23.0)
        self.assertEqual(suggestion["origin"]["lng"], 72.0)
        self.assertEqual(suggestion["destination"]["lat"], 23.1)
        self.assertEqual(suggestion["destination"]["lng"], 72.1)


if __name__ == "__main__":
    unittest.main()
